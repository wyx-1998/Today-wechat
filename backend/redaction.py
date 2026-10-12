"""Fixed P0 text redaction. Original messages are never rewritten."""

import re


RULES = (
    ("api_key", r"(?i)(?:\bsk-[a-z0-9_-]{12,}\b|(?:api[_ -]?key|访问密钥)\s*[:：=]\s*[^\s，。;；]{3,})"),
    ("token", r"(?i)(?:bearer\s+|token\s*[:：=]\s*)[a-z0-9._\-/+]{6,}"),
    ("password", r"(?i)(?:密码|password|passwd)\s*[:：=]\s*[^\s，。;；]{3,}"),
    ("email", r"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"),
    ("id_card", r"(?<!\w)[1-9]\d{16}[\dXx](?!\w)"),
    ("bank_card", r"(?<!\d)\d{16,19}(?!\d)"),
    ("phone", r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
)


def redact(text):
    categories = []
    for kind, pattern in RULES:
        text, count = re.subn(pattern, "[" + kind + "]", text)
        if count:
            categories.append(kind)
    return text, categories
