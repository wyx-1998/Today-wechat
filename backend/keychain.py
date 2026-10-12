"""App-owned macOS Keychain master key; no shell or plaintext fallback."""

import ctypes as C
import os
import sys
import threading

from backend.contracts import ServiceError


class MacKeychain:
    def __init__(self):
        self.lock = threading.Lock()

    def master(self, account, *, create=False):
        if sys.platform != "darwin":
            raise ServiceError("keychain_unavailable", "此版本需要 macOS 钥匙串；不会改为明文保存。")
        with self.lock:
            return self._master(account, create)

    def _master(self, account, create):
        cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sec = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        ptr, integer = C.c_void_p, C.c_long
        cf.CFStringCreateWithCString.argtypes = [ptr, C.c_char_p, C.c_uint32]
        cf.CFStringCreateWithCString.restype = ptr
        cf.CFDictionaryCreateMutable.argtypes = [ptr, integer, ptr, ptr]
        cf.CFDictionaryCreateMutable.restype = ptr
        cf.CFDictionarySetValue.argtypes = [ptr, ptr, ptr]
        cf.CFRelease.argtypes = [ptr]
        cf.CFDataCreate.argtypes = [ptr, ptr, integer]
        cf.CFDataCreate.restype = ptr
        cf.CFDataGetLength.argtypes = [ptr]
        cf.CFDataGetLength.restype = integer
        cf.CFDataGetBytePtr.argtypes = [ptr]
        cf.CFDataGetBytePtr.restype = ptr
        cf.CFGetTypeID.argtypes = [ptr]
        cf.CFGetTypeID.restype = C.c_ulong
        cf.CFDataGetTypeID.restype = C.c_ulong
        sec.SecItemCopyMatching.argtypes = [ptr, C.POINTER(ptr)]
        sec.SecItemCopyMatching.restype = C.c_int32
        sec.SecItemAdd.argtypes = [ptr, ptr]
        sec.SecItemAdd.restype = C.c_int32
        owned = []

        def constant(lib, name):
            return ptr.in_dll(lib, name).value

        def string(value):
            result = cf.CFStringCreateWithCString(None, value.encode("utf-8"), 0x08000100)
            if not result:
                raise ServiceError("keychain_unavailable", "钥匙串参数无法建立，请重试；未保存明文。")
            owned.append(result)
            return result

        def query():
            result = cf.CFDictionaryCreateMutable(None, 0,
                C.addressof((C.c_byte * 1).in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
                C.addressof((C.c_byte * 1).in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
            if not result:
                raise ServiceError("keychain_unavailable", "无法建立钥匙串查询；未保存明文。")
            owned.append(result)
            for name, value in (("kSecClass", constant(sec, "kSecClassGenericPassword")),
                                ("kSecAttrService", string("cn.wechat-today.desktop.model-master")),
                                ("kSecAttrAccount", string(account))):
                cf.CFDictionarySetValue(result, constant(sec, name), value)
            return result

        def read():
            q = query()
            cf.CFDictionarySetValue(q, constant(sec, "kSecReturnData"), constant(cf, "kCFBooleanTrue"))
            cf.CFDictionarySetValue(q, constant(sec, "kSecMatchLimit"), constant(sec, "kSecMatchLimitOne"))
            output = ptr()
            status = sec.SecItemCopyMatching(q, C.byref(output))
            if status == -25300:
                return None
            if status != 0 or not output.value:
                raise ServiceError("keychain_denied", "无法访问应用钥匙串条目，请解锁或允许本应用访问后重试；不会回退明文。")
            owned.append(output.value)
            if cf.CFGetTypeID(output) != cf.CFDataGetTypeID() or cf.CFDataGetLength(output) != 32:
                raise ServiceError("keychain_invalid", "应用主密钥格式不兼容；请保留配置，不要自动重建密钥。")
            return C.string_at(cf.CFDataGetBytePtr(output), 32)

        try:
            result = read()
            if result is not None:
                return result
            if not create:
                raise ServiceError("keychain_missing", "应用主密钥缺失，无法解密已保存配置；请保留数据并重新配置访问密钥。")
            value = os.urandom(32)
            buffer = C.create_string_buffer(value)
            data = cf.CFDataCreate(None, C.cast(buffer, ptr), len(value))
            if not data:
                raise ServiceError("keychain_unavailable", "无法建立主密钥；未保存明文。")
            owned.append(data)
            q = query()
            cf.CFDictionarySetValue(q, constant(sec, "kSecValueData"), data)
            status = sec.SecItemAdd(q, None)
            if status == -25299:  # Another process won creation; read its key.
                return read()
            if status != 0:
                raise ServiceError("keychain_denied", "钥匙串主密钥未保存，请解锁或允许访问后重试；配置未改为明文。")
            return value
        finally:
            for value in reversed(owned):
                if value:
                    cf.CFRelease(value)
