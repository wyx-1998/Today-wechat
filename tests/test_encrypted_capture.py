"""File capture safety with isolated byte fixtures; no private data or models."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.encrypted_capture import capture_pair
from backend.platform_files import capture_open_file
from backend.refresh import RefreshError


class CaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wechat-capture-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.db"
        self.source.write_bytes(b"isolated encrypted fixture" * 500)
        self.wal = Path(str(self.source) + "-wal")
        self.wal.write_bytes(b"isolated wal fixture")
        self.target = self.root / "capture" / "source.db"

    def test_pair_private_not_hard_link_and_later_source_write_isolated(self):
        original = self.source.read_bytes()
        versions, metrics = capture_pair(self.source, self.target)
        self.assertEqual(metrics["attempts"], 1)
        self.assertEqual(metrics["clone_files"] + metrics["copied_files"], 2)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.target.stat().st_ino, self.source.stat().st_ino)
        self.assertEqual(versions[self.source][2], len(original))
        self.source.write_bytes(b"later live write")
        self.wal.write_bytes(b"later wal write")
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(Path(str(self.target) + "-wal").read_bytes(), b"isolated wal fixture")
        self.assertFalse(list(self.target.parent.glob(".capture-pair-*")))

    def test_copy_fallback_keeps_source_and_handles_short_writes(self):
        original_write = os.write
        with patch("backend.platform_files.clone_open_file", return_value=False), patch(
                "backend.platform_files.os.write", side_effect=lambda fd, data: original_write(fd, data[:7])):
            _, metrics = capture_pair(self.source, self.target)
        self.assertEqual(metrics["copied_files"], 2)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())

    def test_source_changes_during_capture_retry_then_accept(self):
        count = 0
        def capture(fd, destination, **kwargs):
            nonlocal count
            result = capture_open_file(fd, destination, **kwargs)
            count += 1
            if count == 1:
                self.source.write_bytes(b"new valid source")
            return result
        with patch("backend.encrypted_capture.capture_open_file", side_effect=capture):
            _, metrics = capture_pair(self.source, self.target)
        self.assertEqual(metrics["attempts"], 2)
        self.assertEqual(self.target.read_bytes(), b"new valid source")

    def test_three_attempts_only_no_published_partial_pair(self):
        attempts = 0
        def capture(fd, destination, **kwargs):
            nonlocal attempts
            result = capture_open_file(fd, destination, **kwargs)
            if destination.name == "source.db":
                attempts += 1
                self.source.write_bytes(str(attempts).encode())
            return result
        with patch("backend.encrypted_capture.capture_open_file", side_effect=capture), self.assertRaisesRegex(RefreshError, "source_changed"):
            capture_pair(self.source, self.target)
        self.assertEqual(attempts, 3)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.target.parent.glob(".capture-pair-*")))

    def test_wal_appearance_and_disappearance_are_retried(self):
        for initially_present in (False, True):
            with self.subTest(present=initially_present):
                if not initially_present:
                    self.wal.unlink()
                target = self.root / str(initially_present) / "source.db"
                changed = False
                def capture(fd, destination, **kwargs):
                    nonlocal changed
                    result = capture_open_file(fd, destination, **kwargs)
                    if not changed:
                        changed = True
                        if initially_present:
                            self.wal.unlink()
                        else:
                            self.wal.write_bytes(b"new wal")
                    return result
                with patch("backend.encrypted_capture.capture_open_file", side_effect=capture):
                    _, metrics = capture_pair(self.source, target)
                self.assertEqual(metrics["attempts"], 2)
                self.assertEqual(Path(str(target) + "-wal").exists(), not initially_present)

    def test_cancel_during_retry_and_fatal_clone_error_do_not_retry(self):
        cancelled = False
        def capture(fd, destination, **kwargs):
            nonlocal cancelled
            result = capture_open_file(fd, destination, **kwargs)
            self.source.write_bytes(b"changed")
            cancelled = True
            return result
        with patch("backend.encrypted_capture.capture_open_file", side_effect=capture), self.assertRaisesRegex(RefreshError, "cancelled"):
            capture_pair(self.source, self.target, cancelled=lambda: cancelled)
        self.assertFalse(self.target.exists())
        with patch("backend.platform_files.clone_open_file", side_effect=PermissionError("isolated denied")) as clone, self.assertRaises(PermissionError):
            capture_pair(self.source, self.target)
        self.assertEqual(clone.call_count, 1)

    def test_existing_destination_and_linked_sources_are_rejected(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b"retained user file")
        with self.assertRaisesRegex(RefreshError, "capture_destination_exists"):
            capture_pair(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"retained user file")
        link = self.root / "linked.db"
        link.symlink_to(self.source)
        with self.assertRaisesRegex(RefreshError, "non_regular_file"):
            capture_pair(link, self.root / "other.db")

    def test_fallback_read_is_bounded_even_when_original_keeps_growing(self):
        expected = self.source.stat().st_size
        read = os.read
        def growing_read(fd, size):
            block = read(fd, size)
            with self.source.open("ab") as writer:
                writer.write(b"new writes" * 100)
            return block
        fd = os.open(self.source, os.O_RDONLY)
        try:
            with patch("backend.platform_files.clone_open_file", return_value=False), patch("backend.platform_files.os.read", side_effect=growing_read):
                self.assertEqual(capture_open_file(fd, self.root / "bounded.db"), "copy")
        finally:
            os.close(fd)
        self.assertEqual((self.root / "bounded.db").stat().st_size, expected)
