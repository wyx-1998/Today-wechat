"""Prepare one disposable, fully validated source set before any reader uses it.

Callers explicitly authorize the source and supply a decoder factory for each
permitted relative DB path. No discovery, key loading, durable publication,
message insertion, cursor advancement or network access occurs here. All
reader connections must close before the context exits. This is a stable set
of captured file versions, not an application-level cross-DB transaction.
"""

from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import stat
import tempfile
import time
from types import MappingProxyType

from backend.refresh import RefreshError, fingerprint, refresh_snapshot
from backend.encrypted_capture import capture_pair


@dataclass(frozen=True)
class PreparedSources:
    root: Path
    source_versions: Mapping[str, tuple | None]
    metrics: Mapping


@contextmanager
def prepare_sources(source_root, working_directory, decoder_factories, *,
                    authorize=False, cancelled=lambda: False, input_guard=lambda: None):
    if authorize is not True:
        raise RefreshError("authorization_required")
    # Validate every entry before a callback or any source DB is opened.
    if not isinstance(decoder_factories, dict) or not decoder_factories:
        raise RefreshError("invalid_source_set")
    relatives = []
    for relative, factory in decoder_factories.items():
        if not isinstance(relative, str) or not callable(factory):
            raise RefreshError("invalid_source_set")
        path = PurePosixPath(relative)
        if (path.is_absolute() or path.as_posix() != relative or len(path.parts) != 2
                or path.parts[0] not in {"message", "contact", "session"}
                or not path.name.endswith(".db") or "\\" in relative or "\0" in relative
                or any(part in {".", ".."} for part in path.parts)):
            raise RefreshError("invalid_source_set")
        relatives.append(relative)
    source, working = Path(source_root).absolute(), Path(working_directory).absolute()
    try:
        if not stat.S_ISDIR(source.lstat().st_mode) or not stat.S_ISDIR(working.lstat().st_mode):
            raise RefreshError("invalid_source_directory")
    except OSError:
        raise RefreshError("invalid_source_directory") from None
    source, working = source.resolve(), working.resolve()
    if source.is_relative_to(working) or working.is_relative_to(source):
        raise RefreshError("source_work_overlap")
    sources = {}
    for relative in sorted(relatives):
        path = source / relative
        if any(part.is_symlink() for part in (path.parent, path)):
            raise RefreshError("non_regular_file")
        sources[path] = fingerprint(path)
        wal = Path(str(path) + "-wal")
        sources[wal] = fingerprint(wal, optional=True)

    def check_guard():
        if cancelled():
            raise RefreshError("cancelled")
        input_guard()  # e.g. a caller-owned check of a selected key file version.
    check_guard()
    with tempfile.TemporaryDirectory(prefix=".wechat-source-set-", dir=working) as directory:
        captured, destination = Path(directory) / "encrypted", Path(directory) / "plain"
        captured.mkdir(mode=0o700)
        destination.mkdir(mode=0o700)
        captured_versions = {}
        metrics = {"skipped": False, "captured_databases": len(relatives), "capture_attempts": 0,
                   "clone_files": 0, "copied_files": 0, "input_bytes": 0, "capture_ms": 0, "decrypt_ms": 0}
        # No decoder executes until every required DB/WAL pair is captured.
        for relative in sorted(relatives):
            check_guard()
            versions, result = capture_pair(source / relative, captured / relative, cancelled=cancelled)
            captured_versions.update(versions)
            for key, result_key in (("capture_attempts", "attempts"), ("clone_files", "clone_files"),
                                    ("copied_files", "copied_files"), ("input_bytes", "input_bytes"), ("capture_ms", "duration_ms")):
                metrics[key] += result[result_key]
        decode_start = time.monotonic()
        for relative in sorted(relatives):
            check_guard()
            # Factories derive page keys from the accepted snapshot, not the
            # changing original. Authentication and WAL checks remain intact.
            original = captured / relative
            decoder = decoder_factories[relative](original)
            if not callable(decoder):
                raise RefreshError("invalid_decoder")
            refresh_snapshot(original, destination / relative, decoder, cancelled=cancelled)
        check_guard()
        metrics["decrypt_ms"] = round((time.monotonic() - decode_start) * 1000, 3)
        versions = MappingProxyType({str(path.relative_to(source)): version for path, version in captured_versions.items()})
        yield PreparedSources(destination, versions, MappingProxyType(metrics))
    # TemporaryDirectory owns only this invocation's tree, not the work root.
