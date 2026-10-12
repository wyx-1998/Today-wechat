"""Platform-independent, process-local credential session. Never persists secrets."""

import sys
import threading
from typing import Protocol

from backend.contracts import ServiceError


class MasterProvider(Protocol):
    def master(self, account: str, *, create: bool = False) -> bytes: ...


class UnsupportedProvider:
    def master(self, account, *, create=False):
        raise ServiceError("keychain_unavailable", "当前系统尚未接入安全凭据存储；不会改为明文保存。")


class CredentialSession:
    """Coalesce unlocks, cache successes, and suppress repeated denied unlocks.

    Provider calls deliberately run outside the condition lock: a system prompt
    must not block close/invalidate. A generation prevents a late result from
    repopulating a cleared session. Python bytes cannot promise secure erasure.
    """

    FAILURE_MESSAGES = {
        "keychain_denied": "无法访问应用凭据，请处理系统权限后主动重试；不会自动反复请求访问。",
        "keychain_missing": "应用主密钥缺失，无法解密配置；不会自动重建或回退明文。",
        "keychain_invalid": "应用主密钥格式不兼容，请保留配置；不会自动替换密钥。",
        "keychain_unavailable": "系统凭据存储不可用，请处理后主动重试；不会回退明文。",
    }

    def __init__(self, provider: MasterProvider):
        self.provider = provider
        self.condition = threading.Condition()
        self.values = {}
        self.failures = {}
        self.pending = set()
        self.generation = 0
        self.closed = False

    def master(self, account, *, create=False):
        if not isinstance(account, str) or not account or type(create) is not bool:
            raise ServiceError("invalid_credential_reference", "凭据引用无效，未访问系统存储。")
        with self.condition:
            generation = self.generation
            while True:
                if self.closed:
                    raise ServiceError("service_closed", "应用凭据会话已关闭，未访问系统存储。")
                if self.generation != generation:
                    raise ServiceError("credential_session_changed", "凭据会话已变化，请主动重试。")
                if account in self.values:
                    return self.values[account]
                if account in self.failures:
                    code = self.failures[account]
                    raise ServiceError(code, self.FAILURE_MESSAGES[code])
                pending = (generation, account)
                if pending not in self.pending:
                    self.pending.add(pending)
                    break
                self.condition.wait()
        value, failure = None, None
        try:
            value = self.provider.master(account, create=create)
            if not isinstance(value, bytes) or len(value) != 32:
                failure = "keychain_invalid"
        except ServiceError as error:
            failure = error.code if error.code in self.FAILURE_MESSAGES else "keychain_unavailable"
        except Exception:
            # Do not propagate provider exception bodies or secret-bearing args.
            failure = "keychain_unavailable"
        finally:
            with self.condition:
                self.pending.discard((generation, account))
                current = not self.closed and self.generation == generation
                if current and (failure or value is not None):
                    if failure:
                        self.failures[account] = failure
                    else:
                        self.values[account] = value
                self.condition.notify_all()
        if not current:
            raise ServiceError("service_closed" if self.closed else "credential_session_changed",
                               "凭据会话已关闭或变化，未复用系统返回值。")
        if failure:
            raise ServiceError(failure, self.FAILURE_MESSAGES[failure])
        return value

    def retry_access(self):
        """Only an explicit user action clears sticky failures; not a timer."""
        with self.condition:
            self.failures.clear()

    def invalidate(self):
        with self.condition:
            self.generation += 1
            self.values.clear()
            self.failures.clear()
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.generation += 1
            self.values.clear()
            self.failures.clear()
            self.condition.notify_all()


def system_credentials():
    if sys.platform == "darwin":
        from backend.keychain import MacKeychain
        provider = MacKeychain()
    else:
        provider = UnsupportedProvider()
    return CredentialSession(provider)
