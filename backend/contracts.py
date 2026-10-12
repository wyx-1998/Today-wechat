"""Versioned local-reader contract, independent of the desktop framework."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json


PROTOCOL_VERSION = 1

# Only application-defined categories, never model text, may enter diagnostics.
DISCOVERY_REJECTION_RULES = (
    "result_fields", "decision_coverage", "decision_fields", "request_unlinked",
    "retained_task_unlinked", "excluded_task_index", "completion_missing_later_evidence",
    "task_fields", "task_conflict", "dependent_task", "change_fields", "change_conflict",
    "change_unlinked", "dependent_change",
)


UNKNOWN_MODEL_RESULTS = frozenset({
    "model_result_unknown", "model_timeout_unknown", "model_connection_closed_unknown",
    "model_connection_reset_unknown", "model_response_incomplete_unknown", "model_dns_unknown",
    "model_tls_unknown", "model_http_protocol_unknown",
})


def unknown_model_result(code):
    """A local timeout does not prove the remote request was not processed."""
    return code in UNKNOWN_MODEL_RESULTS


class ServiceError(Exception):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def payload(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def parse_time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("Missing timezone")
        return result.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise ServiceError("invalid_time", "时间需要包含时区，请重新选择时间范围。") from None


def iso_time(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def stable_id(*parts):
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReadScope:
    account_id: str
    chat_ids: tuple
    time_start: str
    time_end: str

    def __post_init__(self):
        if not self.account_id or not isinstance(self.account_id, str):
            raise ServiceError("invalid_account", "缺少用于区分消息来源的账号标识。")
        if not isinstance(self.chat_ids, tuple) or any(not isinstance(c, str) or not c for c in self.chat_ids):
            raise ServiceError("invalid_scope", "会话范围格式无效，请重新选择会话。")
        if len(self.chat_ids) != len(set(self.chat_ids)):
            raise ServiceError("invalid_scope", "会话范围包含重复会话。")
        if parse_time(self.time_start) >= parse_time(self.time_end):
            raise ServiceError("invalid_time_range", "开始时间需要早于结束时间。")


@dataclass(frozen=True)
class Message:
    message_id: str
    account_id: str
    chat_id: str
    stream_id: str
    local_id: str
    sender_id: str | None
    sender_name: str | None
    is_self: bool | None
    sent_at: str
    message_type: str
    content: str | None
    content_state: str
    analysis_eligible: bool

    def payload(self):
        result = asdict(self)
        content = self.content or ""
        result["content_preview"] = content[:100] + ("…" if len(content) > 100 else "")
        return result


@dataclass(frozen=True)
class ReadBatch:
    chat_id: str
    stream_id: str
    messages: tuple
    next_cursor: str
    has_more: bool
