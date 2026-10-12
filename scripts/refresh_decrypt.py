#!/usr/bin/env python3
"""
refresh_decrypt.py — 增量刷新解密数据库（WAL patch 模式）

首次运行：全量解密配置范围内、已有密钥的 message/contact/session DB。
后续运行：读取变化的 WAL，只重放校验通过且已提交的记录。
主库和 WAL 在临时副本中合并，SQLite 校验与来源稳定性检查通过后原子发布。

原理：
  SQLite WAL 模式下，已提交的新页面可能仍在 .db-wal 文件中。
  检测源文件版本变化 → 验证 WAL → 解密已提交页面 → 校验并发布副本。
  只有 WAL checkpoint 时主 .db 文件才会变，此时需要重新全量解密该 DB。

用法：
  python3 refresh_decrypt.py --config config.yaml          # 增量刷新
  python3 refresh_decrypt.py --config config.yaml --full    # 强制全量解密

依赖：pycryptodome
参考：https://github.com/bbingz/wechat-decrypt (monitor_web.py)
"""
import hashlib
import hmac as hmac_mod
import json
import os
import struct
import sys
import time
import argparse
from pathlib import Path
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.refresh import RefreshError, fingerprint, refresh_snapshot, validate_copy

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80  # IV(16) + HMAC(64)
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24

STATE_FILE_NAME = '.refresh_state.json'

# 只解密这些子目录（collector.py 只用 message/ 和 contact/）
NEEDED_PREFIXES = ('message/', 'contact/', 'session/')


def normalize_rel_path(path):
    """规范化相对路径，跨平台匹配"""
    return path.replace("\\", "/").strip("/")


def derive_mac_key(enc_key, salt):
    """从 enc_key 派生 HMAC 密钥"""
    mac_salt = bytes(b ^ 0x3a for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def verify_page1_hmac(db_path, enc_key):
    """验证 Page 1 HMAC，确认密钥有效。

    返回 True=密钥正确, False=密钥错误/文件损坏。
    失败可能来自密钥不匹配、来源变化或文件损坏，不能仅凭 HMAC 失败判断原因。
    """
    with open(db_path, 'rb') as f:
        page1 = f.read(PAGE_SZ)
    if len(page1) < PAGE_SZ:
        return False

    salt = page1[:SALT_SZ]
    mac_key = derive_mac_key(enc_key, salt)
    hmac_data = page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    stored_hmac = page1[PAGE_SZ - HMAC_SZ: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack('<I', 1))
    return hmac_mod.compare_digest(hm.digest(), stored_hmac)


def decrypt_page(enc_key, page_data, pgno):
    """解密单个加密页面"""
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise RefreshError("crypto_dependency_missing") from None
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return bytes(bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ))
    else:
        encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def authenticated_decoder(db_path, enc_key):
    """Verify every used encrypted page, including committed WAL pages."""
    fingerprint(db_path)
    with open(db_path, 'rb') as original:
        first_page = original.read(PAGE_SZ)
    if len(first_page) != PAGE_SZ or len(enc_key) != KEY_SZ:
        raise RefreshError("encrypted_page_invalid")
    mac_key = derive_mac_key(enc_key, first_page[:SALT_SZ])

    def decode(page, pgno):
        if len(page) != PAGE_SZ:
            raise RefreshError("encrypted_page_invalid")
        start = SALT_SZ if pgno == 1 else 0
        calculated = hmac_mod.new(mac_key, page[start:PAGE_SZ - HMAC_SZ], hashlib.sha512)
        calculated.update(struct.pack('<I', pgno))
        if not hmac_mod.compare_digest(calculated.digest(), page[-HMAC_SZ:]):
            raise RefreshError("page_authentication_failed")
        return decrypt_page(enc_key, page, pgno)

    return decode


def refresh_one(db_path, out_path, enc_key, *, full=True, use_wal=True):
    return refresh_snapshot(db_path, out_path, authenticated_decoder(db_path, enc_key),
                            full=full, use_wal=use_wal)


def full_decrypt_one(db_path, out_path, enc_key):
    """Publish a validated base copy; CLI uses refresh_one for base plus WAL."""
    return refresh_one(db_path, out_path, enc_key, use_wal=False)['base_pages']


def patch_wal(wal_path, out_path, enc_key):
    """Atomically replace a validated copy, replaying committed frames only."""
    wal_path = str(wal_path)
    if not wal_path.endswith('-wal'):
        raise RefreshError("wal_path_invalid")
    if not os.path.exists(wal_path):
        return 0
    return refresh_one(wal_path[:-4], out_path, enc_key, full=False)['committed_frames']


def load_state(state_path):
    """加载上次刷新的 mtime 状态"""
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_state(state_path, state):
    """Publish successful source versions without exposing a partial JSON file."""
    state_path = Path(state_path).absolute()
    fingerprint(state_path, optional=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.refresh-state-', dir=state_path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(state, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, state_path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def get_mtimes(db_path):
    """获取 .db 和 .db-wal 的 mtime"""
    db_mt = os.path.getmtime(db_path) if os.path.exists(db_path) else 0
    wal_mt = os.path.getmtime(db_path + '-wal') if os.path.exists(db_path + '-wal') else 0
    return db_mt, wal_mt


def main():
    parser = argparse.ArgumentParser(description='增量刷新解密数据库')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--full', action='store_true', help='强制全量解密')
    args = parser.parse_args()

    # 加载配置
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypt'))
    from config import load_config
    cfg = load_config(args.config)

    db_dir = cfg['db_dir']
    out_dir = cfg['decrypted_dir']
    keys_file = cfg['keys_file']
    state_path = os.path.join(out_dir, STATE_FILE_NAME)

    if not db_dir or not os.path.isdir(db_dir):
        print(f'[ERROR] 微信数据库目录不存在: {db_dir}', file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(keys_file):
        print(f'[ERROR] 密钥文件不存在: {keys_file}', file=sys.stderr)
        print('请先运行 find_all_keys_macos 提取密钥', file=sys.stderr)
        sys.exit(1)

    with open(keys_file) as f:
        raw_keys = json.load(f)
    # 规范化路径匹配（GitHub 原版做法）
    keys = {}
    for path, value in raw_keys.items():
        if path.startswith('_'):
            continue
        keys[normalize_rel_path(path)] = value

    # 收集需要解密的 DB 文件（只处理 message/contact/session）
    db_files = []
    for root, dirs, files in os.walk(db_dir):
        for fn in files:
            if fn.endswith('.db') and not fn.endswith('-wal') and not fn.endswith('-shm'):
                path = os.path.join(root, fn)
                rel = normalize_rel_path(os.path.relpath(path, db_dir))
                if rel not in keys:
                    continue
                # 只解密 collector.py 需要的子目录
                if not any(rel.startswith(p) for p in NEEDED_PREFIXES):
                    continue
                db_files.append((rel, path))

    if not db_files:
        print('[WARN] 没有找到可解密的数据库文件')
        return

    # Forced rebuild still retains the previous successful version on failure.
    prev_state = load_state(state_path)
    new_state = dict(prev_state)

    t0 = time.perf_counter()
    full_count = 0
    wal_count = 0
    skip_count = 0
    hmac_fail_count = 0
    failure_count = 0
    total_pages = 0

    for rel, src_path in db_files:
        out_path = os.path.join(out_dir, rel)
        wal_path = src_path + '-wal'
        prev = prev_state.get(rel, {})
        try:
            enc_key = bytes.fromhex(keys[rel]['enc_key'])
            current = {'db': list(fingerprint(src_path)),
                       'wal': list(fingerprint(wal_path)) if os.path.exists(wal_path) else None}
            need_full = args.full or not os.path.exists(out_path) or current['db'] != prev.get('db')
            if not need_full:
                fingerprint(out_path)
                try:
                    validate_copy(Path(out_path).absolute())
                except RefreshError:
                    need_full = True
            if not need_full and current == prev:
                skip_count += 1
                continue
            # Base + WAL are built and checked together before one publication.
            result = refresh_one(src_path, out_path, enc_key, full=need_full)
            new_state[rel] = result['source_signatures']
            total_pages += result['base_pages'] + result['committed_frames']
            if need_full:
                full_count += 1
            else:
                wal_count += 1
        except Exception as error:
            failure_count += 1
            code = error.code if isinstance(error, RefreshError) else 'refresh_failed'
            if code == 'page_authentication_failed':
                hmac_fail_count += 1
            # Never log keys, pages or raw exceptions containing source data.
            print(f'[ERROR] 刷新失败 {rel}: {code}；旧副本和成功进度保留', file=sys.stderr)

    elapsed = (time.perf_counter() - t0) * 1000
    save_state(state_path, new_state)

    total = full_count + wal_count + skip_count
    print(
        f'[refresh] {total} 个DB: {full_count} 全量解密, '
        f'{wal_count} WAL patch, {skip_count} 跳过, {failure_count} 失败 | '
        f'{total_pages} 页 | {elapsed:.0f}ms'
    )

    if hmac_fail_count > 0:
        print(
            f'[WARN] {hmac_fail_count} 个 DB 页面认证失败；'
            '请检查来源、密钥是否匹配及文件完整性，不要直接覆盖旧副本',
            file=sys.stderr
        )
        sys.exit(2)  # 特殊退出码，Agent 可据此提醒用户
    if failure_count:
        sys.exit(1)


if __name__ == '__main__':
    main()
