"""将 API 密钥保存在系统钥匙串或进程内存中，不写入配置。"""
import ctypes
import hashlib
import os
import platform
import re
import threading
from pathlib import Path


class CredentialError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("Credentials: " + code)


class MacKeychain:
    def __init__(self):
        if platform.system() != "Darwin":
            raise CredentialError("keychain_unavailable")
        self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer = ctypes.c_void_p
        signatures = {
            "CFDictionaryCreateMutable": ([pointer, ctypes.c_long, pointer, pointer], pointer),
            "CFDictionarySetValue": ([pointer, pointer, pointer], None),
            "CFStringCreateWithCString": ([pointer, ctypes.c_char_p, ctypes.c_uint32], pointer),
            "CFDataCreate": ([pointer, pointer, ctypes.c_long], pointer),
            "CFDataGetLength": ([pointer], ctypes.c_long),
            "CFDataGetBytePtr": ([pointer], pointer),
            "CFRelease": ([pointer], None),
        }
        for name, (args, result) in signatures.items():
            method = getattr(self.cf, name)
            method.argtypes, method.restype = args, result
        for name, args in {"SecItemAdd": [pointer, pointer], "SecItemCopyMatching": [pointer, pointer],
                           "SecItemUpdate": [pointer, pointer], "SecItemDelete": [pointer]}.items():
            method = getattr(self.security, name)
            method.argtypes, method.restype = args, ctypes.c_int32

    def constant(self, name):
        return ctypes.c_void_p.in_dll(self.security, name).value

    def request(self, service, account, operation, value=None):
        # 字典借用这些 CF 对象；保留到请求结束后再统一释放。
        owned = []
        def string(text):
            item = self.cf.CFStringCreateWithCString(None, text.encode(), 0x08000100)
            owned.append(item)
            return item
        def dictionary():
            item = self.cf.CFDictionaryCreateMutable(None, 0, None, None)
            owned.append(item)
            return item
        def put(mapping, key, item):
            self.cf.CFDictionarySetValue(mapping, self.constant(key), item)
        query = dictionary()
        put(query, "kSecClass", self.constant("kSecClassGenericPassword"))
        put(query, "kSecAttrService", string(service))
        put(query, "kSecAttrAccount", string(account))
        result = ctypes.c_void_p()
        try:
            if operation == "get":
                put(query, "kSecReturnData", ctypes.c_void_p.in_dll(self.cf, "kCFBooleanTrue").value)
                put(query, "kSecMatchLimit", self.constant("kSecMatchLimitOne"))
                put(query, "kSecUseAuthenticationUI", self.constant("kSecUseAuthenticationUIFail"))
                status = self.security.SecItemCopyMatching(query, ctypes.byref(result))
                if status == -25300:
                    return ""
                if status == 0:
                    length = self.cf.CFDataGetLength(result)
                    return ctypes.string_at(self.cf.CFDataGetBytePtr(result), length).decode()
            elif operation == "delete":
                status = self.security.SecItemDelete(query)
                if status == -25300:
                    return ""
            else:
                raw = value.encode()
                data = self.cf.CFDataCreate(None, ctypes.c_char_p(raw), len(raw))
                owned.append(data)
                attributes = dictionary()
                put(attributes, "kSecValueData", data)
                status = self.security.SecItemUpdate(query, attributes)
                if status == -25300:
                    put(query, "kSecValueData", data)
                    status = self.security.SecItemAdd(query, None)
            if status:
                raise CredentialError("keychain_locked" if status in [-25308, -128] else "keychain_failed")
            return ""
        finally:
            if result.value:
                self.cf.CFRelease(result)
            for item in reversed(owned):
                self.cf.CFRelease(item)


class Credentials:
    _sessions = {}
    _lock = threading.RLock()

    def __init__(self, root):
        self.service = "org.adaptive-rsi.keys." + hashlib.sha256(str(root).encode()).hexdigest()[:24]
        self.directory = Path(root) / "credentials"

    def put(self, reference, mode, value):
        self.check_reference(reference)
        if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise CredentialError("invalid_key")
        with self._lock:
            if mode == "keychain":
                MacKeychain().request(self.service, reference, "put", value)
            elif mode == "session":
                self._sessions[(self.service, reference)] = value
            elif mode == "file":
                self.directory.mkdir(mode=0o700, exist_ok=True)
                self.directory.chmod(0o700)
                path = self.directory / reference
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(value)
            else:
                raise CredentialError("invalid_mode")

    @staticmethod
    def check_reference(reference):
        if not isinstance(reference, str) or not re.fullmatch(r"[a-f0-9]{32}", reference):
            raise CredentialError("invalid_reference")

    def delete(self, reference, mode):
        self.check_reference(reference)
        with self._lock:
            if mode == "session":
                self._sessions.pop((self.service, reference), None)
            elif mode == "keychain":
                MacKeychain().request(self.service, reference, "delete")
            elif mode == "file":
                (self.directory / reference).unlink(missing_ok=True)

    def get(self, provider):
        mode = provider.get("credential", "env")
        if mode == "none":
            return ""
        if mode == "env":
            name = provider.get("env_key", "")
            value = os.environ.get(name, "") if name else ""
            if name and not value:
                raise CredentialError("missing_key")
            return value
        reference = provider.get("secret_ref", "")
        self.check_reference(reference)
        with self._lock:
            if mode == "file":
                path = self.directory / reference
                if not path.exists():
                    raise CredentialError("missing_key")
                if path.is_symlink() or path.stat().st_mode & 0o077:
                    raise CredentialError("credential_file_unavailable")
                value = path.read_text()
            else:
                value = self._sessions.get((self.service, reference), "") if mode == "session" else MacKeychain().request(self.service, reference, "get")
        if not value:
            raise CredentialError("missing_key")
        return value

    def status(self, provider):
        mode = provider.get("credential", "env")
        if mode == "none" or mode == "env" and not provider.get("env_key"):
            return "not_required"
        if mode == "env":
            return "configured" if os.environ.get(provider.get("env_key", "")) else "missing"
        if mode == "session":
            return "configured" if (self.service, provider.get("secret_ref")) in self._sessions else "missing"
        if mode == "file":
            return "file" if (self.directory / provider.get("secret_ref", "missing")).is_file() else "missing"
        # 页面轮询只返回保存状态，不反复打开系统钥匙串。
        return "stored" if provider.get("secret_ref") else "missing"
