"""Separate macOS synthetic AES checks; never loads private paths or keys.

Run with the pinned requirements-refresh-test.txt environment. Missing
dependencies/platform support fail explicitly, never count as skipped passes.
The fixture encryptor does not call production decoding or WAL checksum code.
This tests the repository's fixed 4096/80/raw-key format, not SQLCipher itself
or any particular private WeChat version.
"""

from contextlib import closing
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest

from backend.refresh import RefreshError
from backend.prepared_sources import prepare_sources
from scripts import refresh_decrypt as refresh


KEY = bytes(range(32))  # Public synthetic material, not a private key.
SALT = bytes(range(16, 32))
LONG_TEXT = "合成文本。" * 560
PAGE_SIZE = 4096
RESERVE = 80
REPOSITORY = Path(__file__).resolve().parents[1]


def fixture_checksum(data, seed=(0, 0), endian="<"):
    words = struct.unpack(endian + str(len(data) // 4) + "I", data)
    left, right = seed
    for offset in range(0, len(words), 2):
        left = (left + words[offset] + right) % (2 ** 32)
        right = (right + words[offset + 1] + left) % (2 ** 32)
    return left, right


def encrypt_page(page, pgno, generation=0):
    from Crypto.Cipher import AES
    assert len(page) == PAGE_SIZE
    # Deterministic public fixture IVs; not a production encryption example.
    iv = hashlib.sha256(struct.pack(">II", pgno, generation) + page).digest()[:16]
    begin = 16 if pgno == 1 else 0
    ciphertext = AES.new(KEY, AES.MODE_CBC, iv).encrypt(page[begin:-RESERVE])
    mac_key = hashlib.pbkdf2_hmac("sha512", KEY, bytes(n ^ 0x3a for n in SALT), 2, 32)
    mac = hmac.new(mac_key, ciphertext + iv + struct.pack("<I", pgno), hashlib.sha512).digest()
    return (SALT if pgno == 1 else b"") + ciphertext + iv + mac


def encode_wal(plain_wal):
    # Recalculate SQLite checksums over encrypted bytes, as SQLCipher does.
    endian = ">" if plain_wal[:4] == bytes.fromhex("377f0683") else "<"
    header = plain_wal[:24]
    checks = fixture_checksum(header, endian=endian)
    result = bytearray(header + struct.pack(">II", *checks))
    for index, offset in enumerate(range(32, len(plain_wal), PAGE_SIZE + 24)):
        frame = plain_wal[offset:offset + PAGE_SIZE + 24]
        assert len(frame) == PAGE_SIZE + 24
        pgno = struct.unpack(">I", frame[:4])[0]
        payload = encrypt_page(frame[24:], pgno, index + 1)
        checks = fixture_checksum(payload, fixture_checksum(frame[:8], checks, endian), endian)
        result.extend(frame[:16] + struct.pack(">II", *checks) + payload)
    return bytes(result)


def repair_wal_checksums(wal):
    """Keep valid WAL checksums after changing a page to test its HMAC."""
    endian = ">" if wal[:4] == bytes.fromhex("377f0683") else "<"
    checks = fixture_checksum(wal[:24], endian=endian)
    output = bytearray(wal[:24] + struct.pack(">II", *checks))
    for offset in range(32, len(wal), PAGE_SIZE + 24):
        frame = wal[offset:offset + PAGE_SIZE + 24]
        checks = fixture_checksum(frame[24:], fixture_checksum(frame[:8], checks, endian), endian)
        output.extend(frame[:16] + struct.pack(">II", *checks) + frame[24:])
    return bytes(output)


class SyntheticEncryptedRefreshTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != "darwin":
            raise RuntimeError("This independent check currently requires macOS libsqlite3")
        temporary = tempfile.TemporaryDirectory(prefix="wechat-encrypted-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plain = self.root / "plain.db"
        # Public C API, not CPython connection memory or a private process.
        lib = ctypes.CDLL("/usr/lib/libsqlite3.dylib")
        lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
        lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.sqlite3_close.argtypes = [ctypes.c_void_p]
        connection = ctypes.c_void_p()
        self.assertEqual(lib.sqlite3_open(os.fsencode(self.plain), ctypes.byref(connection)), 0)
        self.addCleanup(lambda: self.assertEqual(lib.sqlite3_close(connection), 0))
        reserved = ctypes.c_int(RESERVE)
        self.assertEqual(lib.sqlite3_file_control(connection, b"main", 38, ctypes.byref(reserved)), 0)

        def execute(sql):
            self.assertEqual(lib.sqlite3_exec(connection, sql.encode("utf-8"), None, None, None), 0)

        execute("PRAGMA page_size=4096; PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;"
                "CREATE TABLE records(value TEXT); INSERT INTO records VALUES('initial');"
                "PRAGMA wal_checkpoint(TRUNCATE);")
        self.base = self.plain.read_bytes()
        self.assertEqual(self.base[20], RESERVE)
        for value in ("committed-one", LONG_TEXT):
            execute("INSERT INTO records VALUES('" + value + "');")
        plain_wal = Path(str(self.plain) + "-wal").read_bytes()
        self.assertGreater(len(plain_wal), 32)
        self.expected = self.values(self.plain, immutable=False)
        self.assertEqual(self.expected, ["initial", "committed-one", LONG_TEXT])
        (self.root / "encrypted/message").mkdir(parents=True)
        self.source = self.root / "encrypted/message/message_0.db"
        self.source.write_bytes(b"".join(encrypt_page(self.base[offset:offset + PAGE_SIZE], offset // PAGE_SIZE + 1)
                                        for offset in range(0, len(self.base), PAGE_SIZE)))
        self.wal = Path(str(self.source) + "-wal")
        self.wal.write_bytes(encode_wal(plain_wal))
        self.encrypted_base, self.encrypted_wal = self.source.read_bytes(), self.wal.read_bytes()
        self.target = self.root / "published.db"
        self.target.write_bytes(self.base)
        self.old = self.target.read_bytes()

    def values(self, path, immutable=True):
        uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            return [row[0] for row in connection.execute("SELECT value FROM records ORDER BY rowid")]

    def preserved(self):
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse(list(self.root.glob(".wechat-refresh-*")))

    def test_full_base_and_wal_decryption_matches_independent_sqlite_query(self):
        result = refresh.refresh_one(self.source, self.target, KEY)
        self.assertGreater(result["base_pages"], 1)
        self.assertGreater(result["committed_frames"], 0)
        self.assertEqual(self.values(self.target), self.expected)
        self.assertEqual(len(self.values(self.target)[-1]), 2800)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.source.read_bytes(), self.encrypted_base)
        self.assertEqual(self.wal.read_bytes(), self.encrypted_wal)

    def test_base_only_then_incremental_wal_and_repeat(self):
        refresh.full_decrypt_one(self.source, self.target, KEY)
        self.assertEqual(self.values(self.target), ["initial"])
        for _ in range(2):
            self.assertGreater(refresh.patch_wal(self.wal, self.target, KEY), 0)
            self.assertEqual(self.values(self.target), self.expected)

    def test_wrong_key_and_damaged_later_base_page_preserve_old_copy(self):
        with self.assertRaisesRegex(RefreshError, "page_authentication_failed"):
            refresh.refresh_one(self.source, self.target, bytes(reversed(KEY)))
        self.preserved()
        damaged = bytearray(self.encrypted_base)
        damaged[PAGE_SIZE + 100] ^= 1
        self.source.write_bytes(damaged)
        with self.assertRaisesRegex(RefreshError, "page_authentication_failed"):
            refresh.refresh_one(self.source, self.target, KEY)
        self.preserved()

    def test_valid_wal_checksum_but_invalid_page_hmac_preserves_old_copy(self):
        damaged = bytearray(self.encrypted_wal)
        damaged[32 + 24 + 100] ^= 1
        self.wal.write_bytes(repair_wal_checksums(damaged))
        with self.assertRaisesRegex(RefreshError, "page_authentication_failed"):
            refresh.refresh_one(self.source, self.target, KEY)
        self.preserved()

    def test_page_number_is_authenticated_before_actual_cipher(self):
        decode = refresh.authenticated_decoder(self.source, KEY)
        second = self.encrypted_base[PAGE_SIZE:PAGE_SIZE * 2]
        self.assertEqual(decode(second, 2)[:-RESERVE], self.base[PAGE_SIZE:PAGE_SIZE * 2][:-RESERVE])
        with self.assertRaisesRegex(RefreshError, "page_authentication_failed"):
            decode(second, 3)
        self.preserved()

    def test_prepared_encrypted_set_is_only_visible_after_all_dbs_succeed(self):
        working = self.root / "batch-work"
        working.mkdir(mode=0o700)
        other = self.source.with_name("message_1.db")
        other.write_bytes(self.encrypted_base)
        other_wal = Path(str(other) + "-wal")
        other_wal.write_bytes(self.encrypted_wal)
        factories = {"message/" + path.name: (lambda original: refresh.authenticated_decoder(original, KEY))
                     for path in (self.source, other)}
        with prepare_sources(self.source.parent.parent, working, factories, authorize=True) as prepared:
            for relative in factories:
                self.assertEqual(self.values(prepared.root / relative), self.expected)
            ready = prepared.root
        self.assertFalse(ready.exists())
        damaged = bytearray(self.encrypted_wal)
        damaged[32 + 24 + 100] ^= 1
        other_wal.write_bytes(repair_wal_checksums(damaged))
        with self.assertRaisesRegex(RefreshError, "page_authentication_failed"):
            with prepare_sources(self.source.parent.parent, working, factories, authorize=True):
                self.fail("partially decoded set must not become visible")
        self.assertFalse(list(working.glob(".wechat-source-set-*")))
        self.preserved()

    def test_actual_cli_yaml_crypto_failure_state_preservation_and_retry(self):
        keys = self.root / "synthetic-keys.json"
        keys.write_text(json.dumps({"message/message_0.db": {"enc_key": KEY.hex()}}), encoding="utf-8")
        keys.chmod(0o600)
        config = self.root / "synthetic-config.yaml"
        # JSON is valid YAML. Production load_config still parses this file.
        config.write_text(json.dumps({"wechat": {"db_dir": "encrypted", "decrypted_dir": "output",
                                                  "keys_file": keys.name}}), encoding="utf-8")
        def run(full=False):
            command = [sys.executable, str(REPOSITORY / "scripts/refresh_decrypt.py"), "--config", str(config)]
            return subprocess.run(command + (["--full"] if full else []), cwd=self.root,
                                  capture_output=True, text=True, timeout=20)
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.root / "output/message/message_0.db"
        state = self.root / "output/.refresh_state.json"
        saved, published = json.loads(state.read_text()), output.read_bytes()
        self.assertEqual(self.values(output), self.expected)
        repeated = run()
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("1 跳过", repeated.stdout)
        damaged = bytearray(self.encrypted_wal)
        damaged[32 + 24 + 100] ^= 1
        self.wal.write_bytes(repair_wal_checksums(damaged))
        result = run(full=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("page_authentication_failed", result.stderr)
        self.assertNotIn(KEY.hex(), result.stderr + result.stdout)
        self.assertEqual(json.loads(state.read_text()), saved)
        self.assertEqual(output.read_bytes(), published)
        self.wal.write_bytes(self.encrypted_wal)
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.values(output), self.expected)
        self.assertNotEqual(json.loads(state.read_text()), saved)
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
