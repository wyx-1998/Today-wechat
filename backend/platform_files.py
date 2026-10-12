"""Copy an already-open source into an owned private destination; no source writes.

macOS prefers fd-based copy-on-write cloning. Other filesystems/platforms use
bounded ordinary reads. Neither path creates a hard link or touches WeChat.
"""
import ctypes
import errno
import os
import sys

from backend.refresh import RefreshError


def clone_open_file(source_fd, destination):
    if sys.platform != "darwin":
        return False
    library = ctypes.CDLL(None, use_errno=True)
    clone = getattr(library, "fclonefileat", None)
    if clone is None:
        return False
    clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    clone.restype = ctypes.c_int
    parent_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        if clone(source_fd, parent_fd, os.fsencode(destination.name), 0) == 0:
            os.chmod(destination, 0o600)
            return True
        code = ctypes.get_errno()
        if code in {errno.ENOTSUP, errno.EXDEV, errno.EINVAL, errno.ENOSYS}:
            return False
        raise OSError(code, "private snapshot clone failed")
    finally:
        os.close(parent_fd)


def capture_open_file(source_fd, destination, *, cancelled=lambda: False):
    if cancelled():
        raise RefreshError("cancelled")
    if clone_open_file(source_fd, destination):
        method = "clone"
    else:
        target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
            # Bound work to the file size at open; a busy writer must not keep
            # extending this read forever. Pair validation detects the change.
            bytes_left = os.fstat(source_fd).st_size
            while bytes_left:
                if cancelled():
                    raise RefreshError("cancelled")
                block = os.read(source_fd, min(1024 * 1024, bytes_left))
                if not block:
                    raise RefreshError("source_changed")
                bytes_left -= len(block)
                remaining = memoryview(block)
                while remaining:
                    written = os.write(target_fd, remaining)
                    if written <= 0:
                        raise OSError("private snapshot write failed")
                    remaining = remaining[written:]
        finally:
            os.close(target_fd)
        method = "copy"
    if cancelled():
        raise RefreshError("cancelled")
    return method
