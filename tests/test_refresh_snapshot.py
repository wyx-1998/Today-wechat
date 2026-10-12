"""Actual SQLite WAL and injected failures; no private files, keys or AES claim."""

from contextlib import closing
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import hmac
from io import StringIO
import json
import fcntl
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from backend.refresh import RefreshError, committed_wal, refresh_snapshot


def fixture_checksum(data, seed=(0, 0), endian="<"):
    # Fixture writer independent from the production parser; real SQLite WAL
    # in the first test additionally checks against a separate implementation.
    words = struct.unpack(endian + str(len(data) // 4) + "I", data)
    a, b = seed
    for index in range(0, len(words), 2):
        a = (a + words[index] + b) % (2 ** 32)
        b = (b + words[index + 1] + a) % (2 ** 32)
    return a, b


def tail_frame(wal, *, commit=0, salt_change=False):
    size = struct.unpack(">I", wal[8:12])[0]
    last = wal[-(size + 24):]
    header = bytearray(last[:16])
    header[4:8] = struct.pack(">I", commit)
    if salt_change:
        header[8:12] = struct.pack(">I", 42)
    payload = b"X" * size
    seed = struct.unpack(">II", last[16:24])
    byte_order = ">" if wal[:4] == bytes.fromhex("377f0683") else "<"
    checks = fixture_checksum(payload, fixture_checksum(header[:8], seed, byte_order), byte_order)
    return bytes(header) + struct.pack(">II", *checks) + payload


class RefreshSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="wechat-refresh-synthetic-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source.db"
        self.target = self.root / "published.db"
        self.db = sqlite3.connect(self.source)
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA page_size=4096")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.db.execute("CREATE TABLE records(value TEXT)")
        self.db.execute("INSERT INTO records VALUES('initial')")
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copyfile(self.source, self.target)
        self.before = self.target.read_bytes()
        for value in ("committed-one", "committed-two"):
            self.db.execute("INSERT INTO records VALUES(?)", (value,))
            self.db.commit()
        self.wal = Path(str(self.source) + "-wal")
        self.raw_wal = self.wal.read_bytes()

    def refresh(self, **kwargs):
        return refresh_snapshot(self.source, self.target, kwargs.pop("decode", lambda page, _: page), **kwargs)

    def values(self):
        with closing(sqlite3.connect(self.target.as_uri() + "?immutable=1&mode=ro", uri=True)) as db:
            return [row[0] for row in db.execute("SELECT value FROM records")]

    def preserved(self):
        self.assertEqual(self.target.read_bytes(), self.before)
        self.assertFalse(list(self.root.glob(".wechat-refresh-*")))

    def test_actual_sqlite_wal_publishes_commits_and_never_changes_sources(self):
        source_before = self.source.read_bytes()
        for full in (True, False):
            result = self.refresh(full=full)
            self.assertGreater(result["committed_frames"], 0)
            self.assertEqual(self.values(), ["initial", "committed-one", "committed-two"])
            self.assertEqual(self.source.read_bytes(), source_before)
            self.assertEqual(self.wal.read_bytes(), self.raw_wal)
            self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_valid_uncommitted_and_partial_tail_is_not_decoded(self):
        original = committed_wal(self.raw_wal)
        for suffix in (tail_frame(self.raw_wal), b"partial-header"):
            self.wal.write_bytes(self.raw_wal + suffix)
            parsed = committed_wal(self.wal.read_bytes())
            self.assertEqual(parsed, original)
            result = self.refresh()
            self.assertEqual(result["committed_frames"], len(original.frames))
            self.assertEqual(self.values(), ["initial", "committed-one", "committed-two"])

    def test_old_cycle_tail_stops_scan_instead_of_skipping_to_later_frame(self):
        self.wal.write_bytes(self.raw_wal + tail_frame(self.raw_wal, salt_change=True) + tail_frame(self.raw_wal, commit=2))
        self.refresh()
        self.assertEqual(self.values(), ["initial", "committed-one", "committed-two"])

    def test_corrupt_header_or_frame_does_not_publish(self):
        for offset in (0, 24, 32 + 24):
            with self.subTest(offset=offset):
                changed = bytearray(self.raw_wal)
                changed[offset] ^= 1
                self.wal.write_bytes(changed)
                with self.assertRaises(RefreshError):
                    self.refresh()
                self.preserved()

    def test_decoder_failure_and_cancel_leave_old_copy(self):
        def fail(_, pgno):
            if pgno == 2:
                raise RefreshError("page_authentication_failed")
            return self.source.read_bytes()[:4096]
        with self.assertRaisesRegex(RefreshError, "page_authentication_failed"):
            self.refresh(decode=fail)
        self.preserved()
        with self.assertRaisesRegex(RefreshError, "cancelled"):
            self.refresh(cancelled=lambda: True)
        self.preserved()
        # Reach the old-copy open, then cancel at its first chunk checkpoint.
        copying = False
        original_open = Path.open
        def observe_open(path, *args, **kwargs):
            nonlocal copying
            if path == self.target:
                copying = True
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", observe_open), self.assertRaisesRegex(RefreshError, "cancelled"):
            self.refresh(full=False, cancelled=lambda: copying)
        self.assertTrue(copying)
        self.preserved()
        # A cancellation during checksum scanning must precede page decoding.
        scans = 0
        def cancel_during_wal_scan():
            nonlocal scans
            scans += 1
            return scans == 2
        with self.assertRaisesRegex(RefreshError, "cancelled"):
            committed_wal(self.raw_wal, cancelled=cancel_during_wal_scan)
        self.assertEqual(scans, 2)
        self.preserved()

    def test_source_change_before_publish_is_rejected(self):
        changed = False
        def decode(page, _):
            nonlocal changed
            if not changed:
                changed = True
                value = self.source.stat()
                os.utime(self.source, ns=(value.st_atime_ns, value.st_mtime_ns + 1000000))
            return page
        with self.assertRaisesRegex(RefreshError, "source_changed"):
            self.refresh(decode=decode)
        self.preserved()

    def test_replace_failure_busy_lock_and_sidecars_preserve_old_copy(self):
        with patch("backend.refresh.os.replace", side_effect=OSError("synthetic publish failure")):
            with self.assertRaises(OSError):
                self.refresh()
        self.preserved()
        with open(str(self.target) + ".refresh.lock", "r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RefreshError, "refresh_busy"):
                self.refresh()
        Path(str(self.target) + "-wal").write_bytes(b"synthetic sidecar")
        with self.assertRaisesRegex(RefreshError, "copy_sidecar_present"):
            self.refresh()
        self.preserved()

    def test_no_commit_does_not_expose_frame(self):
        header = self.raw_wal[:32]
        size = 4096
        frame_header = self.raw_wal[32:48]
        seed = struct.unpack(">II", header[24:32])
        prefix = frame_header[:4] + b"\0" * 4 + frame_header[8:16]
        endian = ">" if header[:4] == bytes.fromhex("377f0683") else "<"
        payload = b"X" * size
        checks = fixture_checksum(payload, fixture_checksum(prefix[:8], seed, endian), endian)
        self.wal.write_bytes(header + prefix + struct.pack(">II", *checks) + payload)
        result = self.refresh()
        self.assertEqual(result["committed_frames"], 0)
        self.assertEqual(self.values(), ["initial"])

    def test_vacuum_commit_shrinks_copy_to_committed_size(self):
        self.db.execute("INSERT INTO records VALUES(?)", ("synthetic overflow " * 6000,))
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copyfile(self.source, self.target)
        old_size = self.target.stat().st_size
        self.db.execute("DELETE FROM records")
        self.db.commit()
        self.db.execute("VACUUM")
        result = self.refresh(full=False)
        self.assertLess(self.target.stat().st_size, old_size)
        self.assertEqual(self.target.stat().st_size, result["database_pages"] * 4096)
        self.assertEqual(self.values(), [])

    def test_incomplete_source_and_linked_target_are_rejected(self):
        linked = self.root / "linked.db"
        linked.symlink_to(self.target)
        with self.assertRaisesRegex(RefreshError, "non_regular_file"):
            refresh_snapshot(self.source, linked, lambda page, _: page)
        with self.source.open("ab") as output:
            output.write(b"partial page")
        with self.assertRaisesRegex(RefreshError, "source_page_incomplete"):
            self.refresh()
        self.preserved()

    def test_big_endian_wal_checksum_branch_matches_actual_page_results(self):
        header = bytearray(self.raw_wal[:24])
        header[:4] = bytes.fromhex("377f0683")
        checks = fixture_checksum(header, endian=">")
        rebuilt = bytes(header) + struct.pack(">II", *checks)
        for offset in range(32, len(self.raw_wal), 4120):
            frame = self.raw_wal[offset:offset + 4120]
            checks = fixture_checksum(frame[24:], fixture_checksum(frame[:8], checks, ">"), ">")
            rebuilt += frame[:16] + struct.pack(">II", *checks) + frame[24:]
        self.wal.write_bytes(rebuilt)
        self.refresh()
        self.assertEqual(self.values(), ["initial", "committed-one", "committed-two"])

    def test_authenticated_decoder_verifies_page_mac_before_cipher_call(self):
        from scripts import refresh_decrypt as cli
        key = bytes(range(32))  # Public synthetic fixture, never a private key.
        salt = self.source.read_bytes()[:16]
        mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(value ^ 0x3a for value in salt), 2, 32)
        decode = cli.authenticated_decoder(self.source, key)
        with patch.object(cli, "decrypt_page", return_value=b"D" * 4096) as cipher:
            for pgno in (1, 2):
                page = bytearray(b"T" * 4096)
                if pgno == 1:
                    page[:16] = salt
                start = 16 if pgno == 1 else 0
                page[-64:] = hmac.new(mac_key, bytes(page[start:-64]) + struct.pack("<I", pgno), hashlib.sha512).digest()
                self.assertEqual(decode(bytes(page), pgno), b"D" * 4096)
            page[-1] ^= 1
            with self.assertRaisesRegex(RefreshError, "page_authentication_failed"):
                decode(bytes(page), 2)
            self.assertEqual(cipher.call_count, 2)

    def test_cli_failure_keeps_success_versions_and_then_retries(self):
        from scripts import refresh_decrypt as cli
        source_root, output_root = self.root / "cli-source", self.root / "cli-output"
        (source_root / "message").mkdir(parents=True)
        source = source_root / "message/message_0.db"
        source.write_bytes(self.source.read_bytes())
        wal = Path(str(source) + "-wal")
        wal.write_bytes(self.raw_wal)
        keys = self.root / "synthetic-keys.json"
        keys.write_text(json.dumps({"message/message_0.db": {"enc_key": bytes(range(32)).hex()}}), encoding="utf-8")
        configuration = {"db_dir": str(source_root), "decrypted_dir": str(output_root), "keys_file": str(keys)}
        fake_config = SimpleNamespace(load_config=lambda _: configuration)
        with patch.dict(sys.modules, {"config": fake_config}), patch.object(cli, "authenticated_decoder", return_value=lambda page, _: page), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with patch.object(sys, "argv", ["refresh_decrypt", "--config", "synthetic-config"]):
                cli.main()
            target = output_root / "message/message_0.db"
            state = output_root / cli.STATE_FILE_NAME
            saved, original = json.loads(state.read_text()), target.read_bytes()
            changed = bytearray(self.raw_wal)
            changed[56] ^= 1
            wal.write_bytes(changed)
            with patch.object(sys, "argv", ["refresh_decrypt", "--config", "synthetic-config", "--full"]):
                with self.assertRaises(SystemExit) as failure:
                    cli.main()
                self.assertEqual(failure.exception.code, 1)
            self.assertEqual(json.loads(state.read_text()), saved)
            self.assertEqual(target.read_bytes(), original)
            wal.write_bytes(self.raw_wal)
            with patch.object(sys, "argv", ["refresh_decrypt", "--config", "synthetic-config"]):
                cli.main()
            self.assertNotEqual(json.loads(state.read_text()), saved)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
