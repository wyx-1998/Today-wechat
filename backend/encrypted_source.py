"""Explicitly selected key file and disposable encrypted source preparation.

No key extraction, network, path discovery outside the selected root, or
durable plaintext source cache. Settings contain paths, never raw keys.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re

from backend.contracts import ServiceError
from backend.prepared_sources import prepare_sources
from backend.reader import SourceReader
from backend.source_layout import message_shards
from backend.refresh import RefreshError, fingerprint
from backend.encrypted_capture import pair_versions
from scripts.refresh_decrypt import authenticated_decoder

KEY_FILE_LIMIT = 4 * 1024 * 1024  # Parser resource safety, not a message budget.
SNAPSHOT_POLICY = 1


def manifest(versions, key_version):
    # Versions only: never keys, plaintext, messages or durable decrypted paths.
    return {"policy": SNAPSHOT_POLICY, "keys": list(key_version),
            "files": {relative: list(version) if version is not None else None
                      for relative, version in sorted(versions.items())}}


def encrypted_manifest(source):
    """Cheap probe used only against a previously successful sync checkpoint."""
    try:
        root, key_file = selected_paths(source["root"], source["keys_file"])
        keys, key_version = read_keys(key_file)
        files = source_files(root, metadata_only=False)
        if not any(path.parent.name == "message" for path in files):
            raise ServiceError("encrypted_source_empty", "未找到可处理的消息分库。")
        if any(path.relative_to(root).as_posix() not in keys for path in files):
            raise ServiceError("missing_database_key", "所选密钥文件未覆盖本轮数据库，已保存进度保留。")
        versions = {path.relative_to(root).as_posix(): version
                    for database in files for path, version in pair_versions(database).items()}
        if fingerprint(key_file) != key_version:
            raise RefreshError("key_file_changed")
        if source_files(root, metadata_only=False) != files:
            raise RefreshError("source_set_changed")
        return manifest(versions, key_version)
    except RefreshError as error:
        raise source_error(error) from None
    except OSError:
        raise ServiceError("encrypted_source_io", "加密来源暂不可读，已保存进度保留。", retryable=True) from None


def source_files(root, *, metadata_only):
    """Prepare only databases the current SourceReader actually consumes.

    Contact/session FTS indexes may require vendor extensions and are not queried
    by discovery. Never decrypt those unrelated indexes just to enumerate chats.
    Every supported text shard remains required and fail-closed. Other names
    are outside this version's compatibility profile, not inferred as empty.
    """
    files = []
    for folder, filename in (("contact", "contact.db"), ("session", "session.db")):
        parent = root / folder
        if parent.is_symlink():
            raise RefreshError("non_regular_file")
        candidate = parent / filename
        if candidate.is_symlink():
            raise RefreshError("non_regular_file")
        if candidate.exists():
            files.append(candidate)
    if not metadata_only:
        parent = root / "message"
        if parent.is_symlink():
            raise RefreshError("non_regular_file")
        files.extend(message_shards(parent))
    return files


def source_error(error):
    code = error.code
    if code == "cancelled":
        return ServiceError(code, "同步已停止，已保存数据与成功进度保留。", retryable=True)
    if code == "page_authentication_failed":
        message = "数据库页面认证失败。请检查密钥匹配、来源是否变化及文件完整性；已保存数据和进度保留。"
    elif code in {"source_changed", "source_set_changed", "key_file_changed"}:
        message = "来源或密钥文件在准备期间发生变化，未使用本轮副本；请稍后重试。"
    elif code == "crypto_dependency_missing":
        message = "读取程序缺少加密依赖，请使用重新构建的应用；已保存数据保留。"
    else:
        message = "加密来源准备失败。请检查所选目录、密钥文件和数据库格式；已保存数据与进度保留。"
    return ServiceError(code, message, retryable=True)


def selected_paths(root, keys_file):
    if not all(isinstance(value, str) and value for value in (root, keys_file)):
        raise ServiceError("invalid_source", "请选择加密数据库目录和已有 JSON 密钥文件。")
    try:
        source, key = Path(root).expanduser().absolute(), Path(keys_file).expanduser().absolute()
        if source.is_symlink() or key.is_symlink():
            raise OSError()
        source, key = source.resolve(strict=True), key.resolve(strict=True)
        if not source.is_dir() or not (source / "message").is_dir() or (source / "message").is_symlink():
            raise OSError()
    except (OSError, ValueError):
        raise ServiceError("invalid_source", "所选目录或密钥文件不可用，请重新选择；不支持链接来源。") from None
    return source, key


def read_keys(path):
    """Bounded, regular-file read; reject duplicate JSON keys without logging."""
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        version = fingerprint(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            current = os.fstat(file.fileno())
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != version:
                raise RefreshError("key_file_changed")
            payload = file.read(KEY_FILE_LIMIT + 1)
        if fingerprint(path) != version:
            raise RefreshError("key_file_changed")
        if len(payload) > KEY_FILE_LIMIT:
            raise ValueError()
        raw = json.loads(payload, object_pairs_hook=unique_pairs)
        if not isinstance(raw, dict):
            raise ValueError()
        keys = {}
        for relative, value in raw.items():
            if relative.startswith("_"):
                continue
            relative = relative.replace("\\", "/")
            # Other tool entries (e.g. media) are not opened by this product.
            if relative.split("/", 1)[0] not in {"message", "contact", "session"}:
                continue
            if not re.fullmatch(r"(?:message|contact|session)/[^/\\\x00]+\.db", relative) or relative in keys:
                raise ValueError()
            encoded = value.get("enc_key") if isinstance(value, dict) else None
            if not isinstance(encoded, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", encoded):
                raise ValueError()
            keys[relative] = bytes.fromhex(encoded)
        if not keys:
            raise ValueError()
        return keys, version
    except RefreshError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ServiceError("invalid_keys_file", "密钥文件不可读或格式不兼容。需要相对数据库路径及 64 位十六进制 enc_key；不会自动提取密钥。") from None


@contextmanager
def encrypted_reader(source, working, *, metadata_only=False, cancelled=lambda: False):
    try:
        root, key_file = selected_paths(source["root"], source["keys_file"])
        keys, key_version = read_keys(key_file)
        files = source_files(root, metadata_only=metadata_only)
        relatives = [path.relative_to(root).as_posix() for path in files]
        if not files or (not metadata_only and not any(r.startswith("message/") for r in relatives)):
            raise ServiceError("encrypted_source_empty", "未找到可处理的数据库；发现会话需要 contact 或 session 数据库，同步需要 message 分库。")
        if any(relative not in keys for relative in relatives):
            raise ServiceError("missing_database_key", "所选密钥文件未覆盖本轮数据库，未开始读取消息或推进进度；请使用匹配的完整密钥文件。")

        def guard():
            if fingerprint(key_file) != key_version:
                raise RefreshError("key_file_changed")
            current_files = source_files(root, metadata_only=metadata_only)
            if current_files != files:
                raise RefreshError("source_set_changed")

        factories = {relative: (lambda original, key=keys[relative]: authenticated_decoder(original, key))
                     for relative in relatives}
        with prepare_sources(root, working, factories, authorize=True,
                             cancelled=cancelled, input_guard=guard) as prepared:
            # Discovery has no message pages, but SourceReader expects the folder.
            (prepared.root / "message").mkdir(exist_ok=True, mode=0o700)
            reader = SourceReader(prepared.root, source["account_id"], source.get("self_id"))
            reader.source_manifest = manifest(prepared.source_versions, key_version)
            reader.preparation_metrics = dict(prepared.metrics)
            yield reader
    except RefreshError as error:
        raise source_error(error) from None
    except OSError:
        raise ServiceError("encrypted_source_io", "加密来源暂不可读或临时副本无法建立，请检查文件和磁盘空间后重试；已保存数据保留。", retryable=True) from None
